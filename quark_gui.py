import sys
import os
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QTextEdit, QLabel, QFileDialog, QProgressBar
)
from PyQt6.QtCore import pyqtSignal, QObject, QThread, Qt

# Importa le funzioni dal tuo backend
from src.transport import connect_switch
from src.protocol import listen_for_commands

# --- STILI CSS FISSI ---
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
    /* Stile specifico per il tasto rosso di Stop */
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
        max-height: 4px;
    }
    QProgressBar::chunk {
        background-color: #0E639C;
    }
"""


class OutputWrapper(QObject):
    text_written = pyqtSignal(str)

    def write(self, text):
        self.text_written.emit(text)

    def flush(self):
        pass


class ConnectWorker(QThread):
    connected = pyqtSignal(object, object, object)
    error = pyqtSignal(str)

    def run(self):
        try:
            dev, ep_out, ep_in = connect_switch()
            self.connected.emit(dev, ep_out, ep_in)
        except Exception as e:
            self.error.emit(str(e))


class ServerWorker(QThread):
    def __init__(self, dev, ep_out, ep_in, folder):
        super().__init__()
        self.dev = dev
        self.ep_out = ep_out
        self.ep_in = ep_in
        self.folder = folder

    def run(self):
        try:
            listen_for_commands(self.dev, self.ep_out, self.ep_in, self.folder)
        except Exception as e:
            print(f"Server loop error/terminated: {e}")


class PyQuarkApp(QMainWindow):
    def __init__(self):
        super().__init__()
        self.dev = None
        self.ep_out = None
        self.ep_in = None
        self.server_worker = None

        self.init_ui()
        self.setup_logging()

    def init_ui(self):
        self.setWindowTitle("PyQuark MITM Server")
        self.resize(850, 550)
        self.setStyleSheet(MODERN_STYLE)

        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QVBoxLayout(central_widget)
        main_layout.setContentsMargins(20, 20, 20, 20)
        main_layout.setSpacing(15)

        controls_layout = QHBoxLayout()

        self.btn_connect = QPushButton("Connect to Switch")
        self.btn_connect.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_connect.setToolTip("Please open and run Goldleaf on your Nintendo Switch BEFORE clicking here.")
        self.btn_connect.clicked.connect(self.start_connection)

        self.btn_choose = QPushButton("Choose PyQuark Root")
        self.btn_choose.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_choose.setEnabled(False)
        self.btn_choose.setToolTip("Explore remote PC on Goldleaf AFTER choosing a folder.")
        self.btn_choose.clicked.connect(self.choose_folder)

        # Nuovo tasto Stop (nascosto di default)
        self.btn_stop = QPushButton("Stop Connection")
        self.btn_stop.setObjectName("btnStop")  # Aggancia il CSS rosso
        self.btn_stop.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_stop.setToolTip("Click only if installation is finished on Nintendo Switch")
        self.btn_stop.clicked.connect(self.stop_connection)
        self.btn_stop.hide()

        controls_layout.addWidget(self.btn_connect)
        controls_layout.addWidget(self.btn_choose)
        controls_layout.addStretch()  # Spinge il tasto stop tutto a destra
        controls_layout.addWidget(self.btn_stop)

        main_layout.addLayout(controls_layout)

        self.progress_bar = QProgressBar()
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        main_layout.addWidget(self.progress_bar)

        self.log_console = QTextEdit()
        self.log_console.setReadOnly(True)
        self.log_console.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)
        main_layout.addWidget(self.log_console)

        self.status_label = QLabel("Status: Disconnected")
        self.status_label.setStyleSheet("color: #888888;")
        main_layout.addWidget(self.status_label)

    def setup_logging(self):
        self.stdout_wrapper = OutputWrapper()
        self.stdout_wrapper.text_written.connect(self.append_log)
        sys.stdout = self.stdout_wrapper
        sys.stderr = self.stdout_wrapper

    def append_log(self, text):
        self.log_console.insertPlainText(text)
        scrollbar = self.log_console.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    def start_connection(self):
        self.btn_connect.setEnabled(False)
        self.status_label.setText("Status: Attempting connection to Switch...")
        self.status_label.setStyleSheet("color: #E2C08D;")

        self.progress_bar.setRange(0, 0)

        self.conn_worker = ConnectWorker()
        self.conn_worker.connected.connect(self.on_connected)
        self.conn_worker.error.connect(self.on_connection_error)
        self.conn_worker.start()

    def on_connected(self, dev, ep_out, ep_in):
        self.dev = dev
        self.ep_out = ep_out
        self.ep_in = ep_in

        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(100)

        self.status_label.setText("Status: Connected! Awaiting Root Folder selection.")
        self.status_label.setStyleSheet("color: #89D185;")

        self.btn_connect.setText("Connected")
        self.btn_choose.setEnabled(True)
        self.btn_stop.show()  # Rivela il tasto Stop in alto a destra

        print("✅ Switch connected successfully. Please choose a root folder.")

    def on_connection_error(self, error_msg):
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)

        self.status_label.setText("Status: Connection Failed.")
        self.status_label.setStyleSheet("color: #F44747;")
        self.btn_connect.setEnabled(True)

        print(f"❌ Connection Error: {error_msg}")
        print("💡 Make sure your console is plugged in and Goldleaf is waiting for PC connection.")

    def choose_folder(self):
        folder_path = QFileDialog.getExistingDirectory(
            self,
            "Select PyQuark Root Folder",
            os.path.expanduser("~")
        )

        if folder_path:
            self.btn_choose.setEnabled(False)
            self.btn_choose.setText(f"Serving: {os.path.basename(folder_path)}")

            self.status_label.setText(f"Status: Serving commands on '{folder_path}'")
            self.status_label.setStyleSheet("color: #569CD6;")

            self.progress_bar.setRange(0, 0)

            self.server_worker = ServerWorker(self.dev, self.ep_out, self.ep_in, folder_path)
            self.server_worker.start()

    def stop_connection(self):
        """Interrompe forzatamente la comunicazione USB e ripristina la UI."""
        # 1. Ferma il thread di ascolto
        if self.server_worker and self.server_worker.isRunning():
            self.server_worker.terminate()
            self.server_worker.wait()
            self.server_worker = None

        # 2. Rilascia le risorse USB se instanziate
        if self.dev:
            try:
                import usb.util
                usb.util.dispose_resources(self.dev)
            except Exception as e:
                print(f"⚠️ Warning freeing USB resources: {e}")
            finally:
                self.dev = None
                self.ep_out = None
                self.ep_in = None

        # 3. Ripristina lo stato UI
        self.btn_stop.hide()
        self.btn_connect.setText("Connect to Switch")
        self.btn_connect.setEnabled(True)

        self.btn_choose.setText("Choose PyQuark Root")
        self.btn_choose.setEnabled(False)

        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)

        self.status_label.setText("Status: Disconnected")
        self.status_label.setStyleSheet("color: #888888;")

        print("\n🛑 Connection forcefully stopped by user. Ready for a new session.\n")

    def closeEvent(self, event):
        self.stop_connection()  # Assicura la pulizia se si chiude la finestra ad installazione in corso
        sys.stdout = sys.__stdout__
        sys.stderr = sys.__stderr__
        event.accept()


if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = PyQuarkApp()
    window.show()
    sys.exit(app.exec())