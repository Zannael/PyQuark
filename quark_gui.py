import sys
import os
import re
import threading
import shutil
import time
from enum import Enum, auto
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QTextEdit, QLabel, QFileDialog, QProgressBar,
    QFrame, QScrollArea
)
from PyQt6.QtCore import pyqtSignal, QObject, QThread, Qt, QTimer
from PyQt6.QtGui import QTextCharFormat, QColor, QFont

from src.transport import connect_switch
from src.protocol import listen_for_commands


class AppState(Enum):
    APP_IDLE = auto()
    APP_CONNECTING = auto()
    APP_CONNECTED = auto()
    APP_FOLDER_SELECTED = auto()
    APP_SCANNING = auto()
    APP_READY = auto()
    APP_SERVING = auto()
    APP_PROCESSING = auto()
    APP_STOPPING = auto()
    APP_ERROR = auto()


STATE_MESSAGES = {
    AppState.APP_IDLE: ("Disconnected", "#888888"),
    AppState.APP_CONNECTING: ("Connecting to Nintendo Switch...", "#E2C08D"),
    AppState.APP_CONNECTED: ("Connected. Select a root folder.", "#89D185"),
    AppState.APP_FOLDER_SELECTED: ("Root folder selected.", "#569CD6"),
    AppState.APP_SCANNING: ("Indexing game library...", "#C586C0"),
    AppState.APP_READY: ("Ready for DBI/Goldleaf requests.", "#89D185"),
    AppState.APP_SERVING: ("Transfer active.", "#4EC9B0"),
    AppState.APP_PROCESSING: ("Transfer active.", "#4EC9B0"),
    AppState.APP_STOPPING: ("Stopping session...", "#F44747"),
    AppState.APP_ERROR: ("Error occurred", "#F44747"),
}


LIFECYCLE_STEPS = [
    ("1", "Connect to Switch", False),
    ("2", "Select Root Folder", False),
    ("3", "Index Library", False),
    ("4", "Ready for Console", False),
    ("5", "Transferring", False),
    ("6", "Session Ended", False),
]

STATE_TO_STEP = {
    AppState.APP_IDLE: 0,
    AppState.APP_CONNECTING: 0,
    AppState.APP_CONNECTED: 1,
    AppState.APP_FOLDER_SELECTED: 2,
    AppState.APP_SCANNING: 2,
    AppState.APP_READY: 3,
    AppState.APP_SERVING: 4,
    AppState.APP_PROCESSING: 4,
    AppState.APP_STOPPING: 5,
    AppState.APP_ERROR: 5,
}

VISUAL_PREFIX_TOKENS = (
    "⚠️", "❌", "🔄", "✅", "📥", "📖", "📁", "📄", "📋", "🚪",
    "⚡", "🪄", "🗺️", "📦", "▶️", "⏹️", "🛑", "🔍",
)

DBI_READ_RE = re.compile(r"\[DBI\] Reading:\s+(.+?)\s+\(Offset:\s*(\d+),\s*Size:\s*(\d+)\)")
GL_READ_RE = re.compile(r"CMD: ReadFile\((.+?)\s+\|\s+Offset:\s*(\d+),\s*Size:\s*(\d+)\)")


class LogLevel(Enum):
    INFO = auto()
    WARNING = auto()
    ERROR = auto()
    PROGRESS = auto()
    COMMAND = auto()
    SYSTEM = auto()


LOG_COLORS = {
    LogLevel.INFO: "#D4D4D4",
    LogLevel.WARNING: "#DCDCAA",
    LogLevel.ERROR: "#F44747",
    LogLevel.PROGRESS: "#4FC1FF",
    LogLevel.COMMAND: "#C586C0",
    LogLevel.SYSTEM: "#6A9955",
}

LOG_PREFIXES = {
    LogLevel.INFO: "",
    LogLevel.WARNING: "⚠️ ",
    LogLevel.ERROR: "❌ ",
    LogLevel.PROGRESS: "🔄 ",
    LogLevel.COMMAND: "📥 ",
    LogLevel.SYSTEM: "✅ ",
}


class OutputWrapper(QObject):
    text_written = pyqtSignal(str)

    def write(self, text):
        self.text_written.emit(text)

    def flush(self):
        pass


class LogParser(QObject):
    state_changed = pyqtSignal(object, str)
    log_entry = pyqtSignal(str, object)

    BACKEND_PATTERNS = {
        re.compile(r"👂.*Goldleaf.*DBI.*", re.IGNORECASE): (AppState.APP_READY, "Ready. Waiting for console requests"),
        re.compile(r"🔍\s+Virtual file mapping for DBI.*"): (AppState.APP_SCANNING, "Indexing game library"),
        re.compile(r"✅\s+Found\s+\d+\s+available titles.*"): (AppState.APP_READY, "Library indexing complete"),
        re.compile(r"📖\s+CMD:\s+ReadFile|📥\s+CMD:\s+GetFile|📥\s+CMD:\s+GetDirectory|📥\s+CMD:\s+GetDrive|🔍\s+CMD:\s+StatPath"): (AppState.APP_PROCESSING, "Serving request"),
        re.compile(r"📖\s+\[DBI\]\s+Reading:"): (AppState.APP_PROCESSING, "DBI transfer active"),
        re.compile(r"🔄\s+\[VFS\]\s+Opening|🔄\s+\[VFS\].*started for"): (AppState.APP_PROCESSING, "Preparing VFS stream"),
        re.compile(r"🪄\s+.*Virtual header generation|🗺️\s+\[VFS\+XCI\]"): (AppState.APP_PROCESSING, "Preparing XCI virtual header"),
        re.compile(r"📦\s+\[VFS\]\s+Handle.*"): (AppState.APP_PROCESSING, "VFS handle ready"),
        re.compile(r"📋\s+\[DBI\]\s+File list request"): (AppState.APP_READY, "DBI requested file list"),
        re.compile(r"🚪 \[DBI\] Console-closed connection"): (AppState.APP_STOPPING, "Console closed connection"),
        re.compile(r"⚠️|Error|❌"): (None, None),
    }

    COMMAND_PATTERNS = {
        "GetDriveCount": "GetDriveCount",
        "GetDriveInfo": "GetDriveInfo",
        "GetDirectoryCount": "GetDirectoryCount",
        "GetDirectory": "GetDirectory",
        "GetFileCount": "GetFileCount",
        "GetFile": "GetFile",
        "StatPath": "StatPath",
        "ReadFile": "ReadFile",
        "StartFile": "StartFile",
        "EndFile": "EndFile",
        "Delete": "Delete",
    }

    def parse(self, text):
        if not text or text.isspace():
            return

        text = text.strip()

        for pattern, (state, message) in self.BACKEND_PATTERNS.items():
            if pattern.search(text):
                if state:
                    self.state_changed.emit(state, message)
                break

        for cmd_pattern, cmd_name in self.COMMAND_PATTERNS.items():
            if cmd_pattern in text:
                self.log_entry.emit(text, LogLevel.COMMAND)
                return

        if "⚠️" in text or "Warning" in text:
            self.log_entry.emit(text, LogLevel.WARNING)
        elif "❌" in text or "Error" in text:
            self.log_entry.emit(text, LogLevel.ERROR)
        elif any(x in text for x in ["🔄", "⏳", "🔍", "📦"]):
            self.log_entry.emit(text, LogLevel.PROGRESS)
        elif any(x in text for x in ["✅", "👂", "⚡"]):
            self.log_entry.emit(text, LogLevel.SYSTEM)
        else:
            self.log_entry.emit(text, LogLevel.INFO)


class ConnectWorker(QThread):
    connected = pyqtSignal(object, object, object)
    error = pyqtSignal(str)
    progress = pyqtSignal(str)

    def run(self):
        try:
            self.progress.emit("Searching for Nintendo Switch...")
            dev, ep_out, ep_in = connect_switch()
            self.progress.emit("USB connection established")
            self.connected.emit(dev, ep_out, ep_in)
        except Exception as e:
            self.error.emit(str(e))


class ServerWorker(QThread):
    log_message = pyqtSignal(str)
    finished_with_code = pyqtSignal(int)

    def __init__(self, dev, ep_out, ep_in, folder):
        super().__init__()
        self.dev = dev
        self.ep_out = ep_out
        self.ep_in = ep_in
        self.folder = folder
        self._stop_event = threading.Event()

    def run(self):
        old_stdout = sys.stdout
        old_stderr = sys.stderr

        class LogCapture:
            def __init__(self, signal):
                self.signal = signal

            def write(self, text):
                if text.strip():
                    self.signal.emit(text)

            def flush(self):
                pass

        log_capture = LogCapture(self.log_message)
        sys.stdout = log_capture
        sys.stderr = log_capture

        try:
            listen_for_commands(self.dev, self.ep_out, self.ep_in, self.folder, stop_event=self._stop_event)
        except Exception as e:
            self.log_message.emit(f"Server loop error: {e}")
        finally:
            sys.stdout = old_stdout
            sys.stderr = old_stderr
            self.finished_with_code.emit(0)

    def stop(self):
        self._stop_event.set()


class LifecycleIndicator(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.steps = []
        self.current_step = 0
        self._init_ui()

    def _init_ui(self):
        layout = QHBoxLayout(self)
        layout.setSpacing(5)
        layout.setContentsMargins(0, 0, 0, 0)

        for num, text, _ in LIFECYCLE_STEPS:
            step_frame = QFrame()
            step_frame.setFrameShape(QFrame.Shape.Box)
            step_frame.setStyleSheet("""
                QFrame {
                    border: 1px solid #333;
                    border-radius: 4px;
                    background-color: #252526;
                    padding: 5px 10px;
                }
                QFrame.inactive {
                    border-color: #333;
                    color: #666;
                }
                QFrame.active {
                    border-color: #0E639C;
                    color: #4FC1FF;
                }
                QFrame.complete {
                    border-color: #4EC9B0;
                    color: #4EC9B0;
                }
            """)

            step_layout = QHBoxLayout(step_frame)
            step_layout.setContentsMargins(8, 4, 8, 4)
            step_layout.setSpacing(5)

            num_label = QLabel(f"{num}.")
            num_label.setStyleSheet("font-weight: bold;")

            text_label = QLabel(text)
            text_label.setStyleSheet("font-size: 11px;")

            step_layout.addWidget(num_label)
            step_layout.addWidget(text_label)
            step_layout.addStretch()

            self.steps.append({"frame": step_frame, "num": num_label, "text": text_label, "state": "inactive"})
            layout.addWidget(step_frame)

        self._update_visuals()

    def _update_visuals(self):
        for i, step in enumerate(self.steps):
            if i < self.current_step:
                step["state"] = "complete"
                step["frame"].setProperty("class", "complete")
            elif i == self.current_step:
                step["state"] = "active"
                step["frame"].setProperty("class", "active")
            else:
                step["state"] = "inactive"
                step["frame"].setProperty("class", "inactive")
            step["frame"].style().unpolish(step["frame"])
            step["frame"].style().polish(step["frame"])

    def set_step(self, step_index):
        self.current_step = max(0, min(step_index, len(self.steps) - 1))
        self._update_visuals()

    def reset(self):
        self.current_step = 0
        self._update_visuals()


class PyQuarkApp(QMainWindow):
    def __init__(self):
        super().__init__()
        self.dev = None
        self.ep_out = None
        self.ep_in = None
        self.server_worker = None
        self.current_state = AppState.APP_IDLE
        self.selected_folder = ""
        self.session_started_at = None
        self.transfer_started_at = None
        self.total_bytes_sent = 0
        self.request_count = 0
        self.current_file = "-"
        self.first_transfer_seen = False
        self.session_summary_logged = False

        self.init_ui()
        self.setup_logging()
        self.warn_missing_unrar()
        self.update_state(AppState.APP_IDLE)

    def warn_missing_unrar(self):
        unrar_cmd = os.environ.get("PYQUARK_UNRAR_CMD", "unrar")
        if shutil.which(unrar_cmd) is None:
            self.append_log(
                f"'{unrar_cmd}' not found in PATH. RAR staging mode won't be available.",
                LogLevel.WARNING,
            )

    def init_ui(self):
        self.setWindowTitle("PyQuark MITM Server")
        self.resize(900, 600)
        self.setStyleSheet(MODERN_STYLE)

        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QVBoxLayout(central_widget)
        main_layout.setContentsMargins(20, 20, 20, 20)
        main_layout.setSpacing(12)

        title_label = QLabel("PyQuark - Nintendo Switch File Server")
        title_label.setStyleSheet("font-size: 18px; font-weight: bold; color: #4EC9B0; margin-bottom: 5px;")
        main_layout.addWidget(title_label)

        self.lifecycle = LifecycleIndicator()
        main_layout.addWidget(self.lifecycle)

        controls_container = QWidget()
        controls_layout = QHBoxLayout(controls_container)
        controls_layout.setContentsMargins(0, 0, 0, 0)

        self.btn_connect = QPushButton("🔌 Connect to Switch")
        self.btn_connect.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_connect.setToolTip("Please open and run Goldleaf on your Nintendo Switch BEFORE clicking here.")
        self.btn_connect.clicked.connect(self.start_connection)

        self.btn_choose = QPushButton("📁 Choose PyQuark Root")
        self.btn_choose.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_choose.setEnabled(False)
        self.btn_choose.setToolTip("Explore remote PC on Goldleaf AFTER choosing a folder.")
        self.btn_choose.clicked.connect(self.choose_folder)

        self.btn_stop = QPushButton("⏹️ Stop Connection")
        self.btn_stop.setObjectName("btnStop")
        self.btn_stop.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_stop.setToolTip("Click only if installation is finished on Nintendo Switch")
        self.btn_stop.clicked.connect(self.stop_connection)
        self.btn_stop.hide()

        controls_layout.addWidget(self.btn_connect)
        controls_layout.addWidget(self.btn_choose)
        controls_layout.addStretch()
        controls_layout.addWidget(self.btn_stop)

        main_layout.addWidget(controls_container)

        self.status_container = QWidget()
        status_layout = QHBoxLayout(self.status_container)
        status_layout.setContentsMargins(0, 0, 0, 0)

        self.status_label = QLabel("Status: Disconnected")
        self.status_label.setStyleSheet("color: #888888; font-weight: bold;")

        self.operation_label = QLabel("")
        self.operation_label.setStyleSheet("color: #666666; font-style: italic;")

        status_layout.addWidget(self.status_label)
        status_layout.addStretch()
        status_layout.addWidget(self.operation_label)

        main_layout.addWidget(self.status_container)

        metrics_container = QWidget()
        metrics_layout = QHBoxLayout(metrics_container)
        metrics_layout.setContentsMargins(0, 0, 0, 0)
        metrics_layout.setSpacing(12)

        self.metric_elapsed = QLabel("Elapsed: 00:00")
        self.metric_requests = QLabel("Requests: 0")
        self.metric_bytes = QLabel("Data sent: 0 B")
        self.metric_speed = QLabel("Speed: 0 B/s")
        self.metric_file = QLabel("File: -")
        self.metric_file.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)

        for metric in (
            self.metric_elapsed,
            self.metric_requests,
            self.metric_bytes,
            self.metric_speed,
            self.metric_file,
        ):
            metric.setStyleSheet("color: #9CDCFE;")
            metrics_layout.addWidget(metric)

        metrics_layout.addStretch()
        main_layout.addWidget(metrics_container)

        self.progress_bar = QProgressBar()
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        main_layout.addWidget(self.progress_bar)

        log_header = QLabel("Console Output")
        log_header.setStyleSheet("font-weight: bold; color: #888;")
        main_layout.addWidget(log_header)

        self.log_console = QTextEdit()
        self.log_console.setReadOnly(True)
        self.log_console.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)
        self.log_console.setStyleSheet("""
            QTextEdit {
                background-color: #1E1E1E;
                border: 1px solid #333333;
                border-radius: 5px;
                padding: 5px;
                font-family: 'Consolas', 'Courier New', monospace;
                font-size: 12px;
            }
        """)
        main_layout.addWidget(self.log_console, 1)

    def setup_logging(self):
        self.stdout_wrapper = OutputWrapper()
        self.stdout_wrapper.text_written.connect(self.handle_backend_output)

        self.log_formats = {}
        for level, color in LOG_COLORS.items():
            fmt = QTextCharFormat()
            fmt.setForeground(QColor(color))
            self.log_formats[level] = fmt

        self.info_format = QTextCharFormat()
        self.info_format.setForeground(QColor("#D4D4D4"))

        self.log_parser = LogParser()
        self.log_parser.state_changed.connect(self.on_backend_state_change)
        self.log_parser.log_entry.connect(self.append_log)

        self.metrics_timer = QTimer(self)
        self.metrics_timer.setInterval(1000)
        self.metrics_timer.timeout.connect(self.refresh_metrics)

        self.log_parser.log_entry.emit("PyQuark GUI initialized. Ready to connect.", LogLevel.SYSTEM)

    def handle_backend_output(self, text):
        if text and not text.isspace():
            self.log_parser.parse(text)

    def on_backend_state_change(self, state, message):
        try:
            app_state = AppState[state.name]
            self._transition_state_from_backend(app_state)
            self.update_operation(message)
        except:
            pass

    def _transition_state_from_backend(self, next_state):
        if next_state in (AppState.APP_STOPPING, AppState.APP_ERROR):
            self.update_state(next_state)
            return

        current_step = STATE_TO_STEP.get(self.current_state, 0)
        next_step = STATE_TO_STEP.get(next_state, 0)
        if next_step >= current_step:
            self.update_state(next_state)

    def update_state(self, new_state):
        self.current_state = new_state
        message, color = STATE_MESSAGES.get(new_state, ("Unknown", "#888888"))
        self.status_label.setText(f"Status: {message}")
        self.status_label.setStyleSheet(f"color: {color}; font-weight: bold;")
        self.lifecycle.set_step(STATE_TO_STEP.get(new_state, 0))

        if new_state == AppState.APP_CONNECTING:
            self.progress_bar.setRange(0, 0)
        elif new_state == AppState.APP_CONNECTED:
            self.progress_bar.setRange(0, 100)
            self.progress_bar.setValue(100)
            self.btn_connect.setText("🔌 Connected")
            self.btn_choose.setEnabled(True)
            self.btn_stop.show()
        elif new_state == AppState.APP_FOLDER_SELECTED:
            self.progress_bar.setRange(0, 0)
        elif new_state == AppState.APP_SCANNING:
            self.progress_bar.setRange(0, 0)
        elif new_state == AppState.APP_READY:
            self.progress_bar.setRange(0, 100)
            self.progress_bar.setValue(100)
        elif new_state == AppState.APP_SERVING:
            self.progress_bar.setRange(0, 0)
        elif new_state == AppState.APP_PROCESSING:
            self.progress_bar.setRange(0, 0)
        elif new_state == AppState.APP_STOPPING:
            self.progress_bar.setRange(0, 0)
        elif new_state == AppState.APP_IDLE:
            self.progress_bar.setRange(0, 100)
            self.progress_bar.setValue(0)
            self.lifecycle.reset()

    def update_operation(self, message):
        self.operation_label.setText(message)

    def append_log(self, text, level=LogLevel.INFO):
        if not text or text.isspace():
            return

        cursor = self.log_console.textCursor()
        cursor.movePosition(cursor.MoveOperation.End)

        clean_text = text.rstrip("\n")
        prefix = "" if clean_text.startswith(VISUAL_PREFIX_TOKENS) else LOG_PREFIXES.get(level, "")
        full_text = f"{prefix}{clean_text}"

        if level in self.log_formats:
            cursor.insertText(full_text + "\n", self.log_formats[level])
        else:
            cursor.insertText(full_text + "\n", self.info_format)

        scrollbar = self.log_console.verticalScrollBar()
        if scrollbar:
            scrollbar.setValue(scrollbar.maximum())

    def start_connection(self):
        self.btn_connect.setEnabled(False)
        self.update_state(AppState.APP_CONNECTING)
        self.append_log("Initiating connection to Nintendo Switch...", LogLevel.PROGRESS)

        self.conn_worker = ConnectWorker()
        self.conn_worker.progress.connect(lambda m: self.append_log(m, LogLevel.PROGRESS))
        self.conn_worker.connected.connect(self.on_connected)
        self.conn_worker.error.connect(self.on_connection_error)
        self.conn_worker.start()

    def on_connected(self, dev, ep_out, ep_in):
        self.dev = dev
        self.ep_out = ep_out
        self.ep_in = ep_in

        self.update_state(AppState.APP_CONNECTED)
        self.append_log("Switch connected successfully!", LogLevel.SYSTEM)
        self.append_log("Please choose a root folder to serve.", LogLevel.INFO)

    def on_connection_error(self, error_msg):
        self.update_state(AppState.APP_ERROR)
        self.append_log(f"Connection failed: {error_msg}", LogLevel.ERROR)
        self.append_log("Make sure your console is plugged in and Goldleaf is waiting for PC connection.", LogLevel.WARNING)
        self.btn_connect.setEnabled(True)

    def choose_folder(self):
        folder_path = QFileDialog.getExistingDirectory(
            self,
            "Select PyQuark Root Folder",
            os.path.expanduser("~"),
            options=QFileDialog.Option.DontUseNativeDialog  # <-- AGGIUNGI QUESTA RIGA
        )

        if folder_path:
            self.selected_folder = folder_path
            self._reset_session_metrics()
            self.session_started_at = time.monotonic()
            self.metrics_timer.start()
            self.btn_choose.setEnabled(False)
            self.btn_choose.setText(f"📁 {os.path.basename(folder_path)}")

            self.update_state(AppState.APP_FOLDER_SELECTED)
            self.update_operation(f"Serving: {folder_path}")
            self.append_log(f"Starting server with root folder: {folder_path}", LogLevel.INFO)

            self.update_state(AppState.APP_SCANNING)
            self.append_log("Scanning VFS structure...", LogLevel.PROGRESS)

            self.server_worker = ServerWorker(self.dev, self.ep_out, self.ep_in, folder_path)
            self.server_worker.log_message.connect(self.handle_server_log)
            self.server_worker.finished_with_code.connect(self.on_server_finished)
            self.server_worker.start()

    def handle_server_log(self, text):
        if text and not text.isspace():
            self._ingest_runtime_event(text.strip())
            self.log_parser.parse(text)

    def _ingest_runtime_event(self, text):
        if "CMD:" in text or "[DBI] Reading:" in text:
            self.request_count += 1

        dbi_match = DBI_READ_RE.search(text)
        if dbi_match:
            self.current_file = os.path.basename(dbi_match.group(1).strip())
            self.total_bytes_sent += int(dbi_match.group(3))
            if not self.first_transfer_seen:
                self.first_transfer_seen = True
                self.transfer_started_at = time.monotonic()
                self.update_state(AppState.APP_PROCESSING)
                self.append_log("Transfer started. Streaming data to console.", LogLevel.SYSTEM)
            return

        gl_match = GL_READ_RE.search(text)
        if gl_match:
            self.current_file = gl_match.group(1).strip()
            self.total_bytes_sent += int(gl_match.group(3))
            if not self.first_transfer_seen:
                self.first_transfer_seen = True
                self.transfer_started_at = time.monotonic()
                self.update_state(AppState.APP_PROCESSING)
                self.append_log("Transfer started. Streaming data to console.", LogLevel.SYSTEM)

    @staticmethod
    def _format_bytes(value):
        units = ["B", "KB", "MB", "GB", "TB"]
        amount = float(value)
        idx = 0
        while amount >= 1024 and idx < len(units) - 1:
            amount /= 1024.0
            idx += 1
        if idx == 0:
            return f"{int(amount)} {units[idx]}"
        return f"{amount:.2f} {units[idx]}"

    @staticmethod
    def _format_duration(seconds):
        seconds = max(0, int(seconds))
        minutes, sec = divmod(seconds, 60)
        hours, minutes = divmod(minutes, 60)
        if hours > 0:
            return f"{hours:02d}:{minutes:02d}:{sec:02d}"
        return f"{minutes:02d}:{sec:02d}"

    def refresh_metrics(self):
        now = time.monotonic()
        elapsed = 0 if self.session_started_at is None else (now - self.session_started_at)
        transfer_elapsed = 0 if self.transfer_started_at is None else max(0.001, now - self.transfer_started_at)
        speed = self.total_bytes_sent / transfer_elapsed if self.transfer_started_at else 0

        self.metric_elapsed.setText(f"Elapsed: {self._format_duration(elapsed)}")
        self.metric_requests.setText(f"Requests: {self.request_count}")
        self.metric_bytes.setText(f"Data sent: {self._format_bytes(self.total_bytes_sent)}")
        self.metric_speed.setText(f"Speed: {self._format_bytes(speed)}/s")
        self.metric_file.setText(f"File: {self.current_file}")

    def _reset_session_metrics(self):
        self.session_started_at = None
        self.transfer_started_at = None
        self.total_bytes_sent = 0
        self.request_count = 0
        self.current_file = "-"
        self.first_transfer_seen = False
        self.session_summary_logged = False
        self.refresh_metrics()

    def _append_session_summary(self):
        if self.session_summary_logged:
            return
        if self.session_started_at is None and self.request_count == 0:
            return
        now = time.monotonic()
        elapsed = 0 if self.session_started_at is None else (now - self.session_started_at)
        self.append_log(
            (
                "Session summary -> "
                f"duration: {self._format_duration(elapsed)}, "
                f"requests: {self.request_count}, "
                f"data sent: {self._format_bytes(self.total_bytes_sent)}, "
                f"last file: {self.current_file}"
            ),
            LogLevel.SYSTEM,
        )
        self.session_summary_logged = True

    def on_server_finished(self, code):
        self.metrics_timer.stop()
        self.refresh_metrics()
        self._append_session_summary()
        self.append_log(f"Server stopped (exit code: {code})", LogLevel.WARNING)
        self.reset_ui()

    def stop_connection(self):
        self.update_state(AppState.APP_STOPPING)
        self.append_log("Stopping session...", LogLevel.WARNING)
        self.update_operation("Releasing USB resources...")

        if self.server_worker and self.server_worker.isRunning():
            self.server_worker.stop()
            if not self.server_worker.wait(2500):
                self.append_log("Worker did not stop in time, forcing termination.", LogLevel.WARNING)
                self.server_worker.terminate()
                self.server_worker.wait(2000)
            self.server_worker = None

        self.append_log("Releasing USB resources...", LogLevel.PROGRESS)

        if self.dev:
            try:
                import usb.util
                usb.util.dispose_resources(self.dev)
            except Exception as e:
                self.append_log(f"Warning freeing USB resources: {e}", LogLevel.WARNING)
            finally:
                self.dev = None
                self.ep_out = None
                self.ep_in = None

        self.append_log("Session terminated.", LogLevel.SYSTEM)
        self.metrics_timer.stop()
        self.refresh_metrics()
        self._append_session_summary()
        self.reset_ui()

    def reset_ui(self):
        self.update_state(AppState.APP_IDLE)
        self.update_operation("")

        self.btn_stop.hide()
        self.btn_connect.setText("🔌 Connect to Switch")
        self.btn_connect.setEnabled(True)

        self.btn_choose.setText("📁 Choose PyQuark Root")
        self.btn_choose.setEnabled(False)

        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)

        self.selected_folder = ""
        self._reset_session_metrics()

    def closeEvent(self, event):
        if self.current_state not in (AppState.APP_IDLE, AppState.APP_ERROR):
            self.stop_connection()
        sys.stdout = sys.__stdout__
        sys.stderr = sys.__stderr__
        event.accept()


MODERN_STYLE = """
    QMainWindow {
        background-color: #1E1E1E;
    }
    QWidget {
        color: #D4D4D4;
        font-family: 'Segoe UI', Helvetica, Arial, sans-serif;
    }
    QPushButton {
        background-color: #0E639C;
        color: white;
        border: none;
        padding: 10px 20px;
        border-radius: 5px;
        font-weight: bold;
        font-size: 14px;
    }
    QPushButton:hover {
        background-color: #1177BB;
    }
    QPushButton:pressed {
        background-color: #094771;
    }
    QPushButton:disabled {
        background-color: #3C3C3C;
        color: #888888;
    }
    QPushButton#btnStop {
        background-color: #D32F2F;
    }
    QPushButton#btnStop:hover {
        background-color: #B71C1C;
    }
    QPushButton#btnStop:pressed {
        background-color: #7F0000;
    }
    QTextEdit {
        background-color: #252526;
        border: 1px solid #333333;
        border-radius: 5px;
        padding: 10px;
        font-family: 'Consolas', 'Courier New', monospace;
        font-size: 13px;
    }
    QScrollBar:vertical {
        background: #1E1E1E;
        width: 14px;
        margin: 0px;
    }
    QScrollBar::handle:vertical {
        background: #424242;
        min-height: 20px;
        border-radius: 7px;
        margin: 2px;
    }
    QScrollBar::handle:vertical:hover {
        background: #4F4F4F;
    }
    QLabel {
        font-size: 13px;
    }
    QToolTip {
        background-color: #2D2D30;
        color: #D4D4D4;
        border: 1px solid #555555;
        padding: 5px;
        font-size: 12px;
    }
    QProgressBar {
        border: 1px solid #333333;
        border-radius: 3px;
        text-align: center;
        background-color: #252526;
        max-height: 6px;
    }
    QProgressBar::chunk {
        background-color: #0E639C;
    }
    QFrame {
        background-color: transparent;
    }
"""


if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = PyQuarkApp()
    window.show()
    sys.exit(app.exec())
