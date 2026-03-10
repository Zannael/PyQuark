import sys
import os
import re
import threading
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
    AppState.APP_CONNECTED: ("Connected! Awaiting folder selection.", "#89D185"),
    AppState.APP_FOLDER_SELECTED: ("Folder selected. Preparing VFS...", "#569CD6"),
    AppState.APP_SCANNING: ("Scanning VFS structure...", "#C586C0"),
    AppState.APP_READY: ("Ready. Waiting for commands from console...", "#89D185"),
    AppState.APP_SERVING: ("Serving files to console...", "#4EC9B0"),
    AppState.APP_PROCESSING: ("Processing request...", "#DCDCAA"),
    AppState.APP_STOPPING: ("Stopping session...", "#F44747"),
    AppState.APP_ERROR: ("Error occurred", "#F44747"),
}


LIFECYCLE_STEPS = [
    ("1", "Connect to Switch", False),
    ("2", "Choose Root Folder", False),
    ("3", "Start Serving", False),
    ("4", "Waiting for Commands", False),
    ("5", "Process Commands", False),
    ("6", "Stop Session", False),
]


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
        re.compile(r"👂 Listeining.*"): (AppState.APP_READY, "Waiting for Goldleaf/DBI commands"),
        re.compile(r"🔍 Virtual file mapping for DBI.*"): (AppState.APP_SCANNING, "Scanning VFS structure"),
        re.compile(r"✅ Found \d+ available titles.*"): (AppState.APP_READY, "VFS scan complete"),
        re.compile(r"📖 CMD: ReadFile|📥 CMD: GetFile|📥 CMD: GetDirectory|📥 CMD: GetDrive|🔍 CMD: StatPath"): (AppState.APP_PROCESSING, "Processing command"),
        re.compile(r"📖 \[DBI\] Reading:"): (AppState.APP_PROCESSING, "DBI: Streaming file"),
        re.compile(r"🔄 \[VFS\] Opening|🔄 \[VFS\].*started for"): (AppState.APP_PROCESSING, "Opening VFS file"),
        re.compile(r"🪄 .*Virtual header generation|🗺️ \[VFS\+XCI\]"): (AppState.APP_PROCESSING, "Processing XCI virtual header"),
        re.compile(r"📦 \[VFS\] Handle creato.*"): (AppState.APP_PROCESSING, "VFS handle created"),
        re.compile(r"📋 \[DBI\] File list request"): (AppState.APP_PROCESSING, "DBI: File list request"),
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
            listen_for_commands(self.dev, self.ep_out, self.ep_in, self.folder)
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

        self.init_ui()
        self.setup_logging()
        self.update_state(AppState.APP_IDLE)

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

        self.log_parser = LogParser()
        self.log_parser.state_changed.connect(self.on_backend_state_change)
        self.log_parser.log_entry.emit("PyQuark GUI initialized. Ready to connect.", LogLevel.SYSTEM)

        self.log_formats = {}
        for level, color in LOG_COLORS.items():
            fmt = QTextCharFormat()
            fmt.setForeground(QColor(color))
            self.log_formats[level] = fmt

        self.info_format = QTextCharFormat()
        self.info_format.setForeground(QColor("#D4D4D4"))

    def handle_backend_output(self, text):
        if text and not text.isspace():
            self.log_parser.parse(text)

    def on_backend_state_change(self, state, message):
        try:
            app_state = AppState[state.name]
            self.update_operation(message)
        except:
            pass

    def update_state(self, new_state):
        self.current_state = new_state
        message, color = STATE_MESSAGES.get(new_state, ("Unknown", "#888888"))
        self.status_label.setText(f"Status: {message}")
        self.status_label.setStyleSheet(f"color: {color}; font-weight: bold;")

        if new_state == AppState.APP_CONNECTING:
            self.progress_bar.setRange(0, 0)
            self.lifecycle.set_step(0)
        elif new_state == AppState.APP_CONNECTED:
            self.progress_bar.setRange(0, 100)
            self.progress_bar.setValue(100)
            self.lifecycle.set_step(1)
            self.btn_connect.setText("🔌 Connected")
            self.btn_choose.setEnabled(True)
            self.btn_stop.show()
        elif new_state == AppState.APP_FOLDER_SELECTED:
            self.progress_bar.setRange(0, 0)
            self.lifecycle.set_step(2)
        elif new_state == AppState.APP_SCANNING:
            self.lifecycle.set_step(2)
        elif new_state == AppState.APP_READY:
            self.progress_bar.setRange(0, 100)
            self.progress_bar.setValue(100)
            self.lifecycle.set_step(3)
        elif new_state == AppState.APP_SERVING:
            self.lifecycle.set_step(4)
        elif new_state == AppState.APP_PROCESSING:
            self.lifecycle.set_step(4)
        elif new_state == AppState.APP_STOPPING:
            self.progress_bar.setRange(0, 0)
            self.lifecycle.set_step(5)
        elif new_state == AppState.APP_IDLE:
            self.progress_bar.setRange(0, 100)
            self.progress_bar.setValue(0)
            self.lifecycle.reset()

    def update_operation(self, message):
        self.operation_label.setText(message)
        QTimer.singleShot(5000, lambda: self.operation_label.setText(""))

    def append_log(self, text, level=LogLevel.INFO):
        if not text or text.isspace():
            return

        cursor = self.log_console.textCursor()
        cursor.movePosition(cursor.MoveOperation.End)

        prefix = LOG_PREFIXES.get(level, "")
        full_text = f"{prefix}{text}"

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
            self.log_parser.parse(text)

    def on_server_finished(self, code):
        self.append_log(f"Server stopped (exit code: {code})", LogLevel.WARNING)
        self.reset_ui()

    def stop_connection(self):
        self.update_state(AppState.APP_STOPPING)
        self.append_log("Stopping session...", LogLevel.WARNING)
        self.update_operation("Releasing USB resources...")

        if self.server_worker and self.server_worker.isRunning():
            self.server_worker.stop()
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
