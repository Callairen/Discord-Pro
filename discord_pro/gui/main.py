"""Desktop GUI entry point: single GUI instance, backend launcher, main window."""

from __future__ import annotations

import sys
import threading

from PySide6.QtCore import QObject, Signal
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import QApplication, QMessageBox, QProgressDialog

from discord_pro import paths

GUI_SERVER_NAME = "discord-pro-gui"


def _instance_name() -> str:
    # One GUI per data directory (tests and portable installs use separate dirs).
    import hashlib

    return f"{GUI_SERVER_NAME}-{hashlib.sha1(str(paths.app_data_dir()).encode()).hexdigest()[:10]}"


def _signal_existing_gui() -> bool:
    sock = QLocalSocket()
    sock.connectToServer(_instance_name())
    if sock.waitForConnected(300):
        sock.write(b"show")
        sock.flush()
        sock.waitForBytesWritten(300)
        sock.disconnectFromServer()
        return True
    return False


class _Starter(QObject):
    done = Signal(object, str)

    def run(self) -> None:
        from discord_pro.local_client import ensure_backend

        try:
            self.done.emit(ensure_backend(), "")
        except Exception as exc:  # reported to the user
            self.done.emit(None, str(exc))


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("Discord-Pro")
    app.setQuitOnLastWindowClosed(False)  # closing the window only hides it

    if _signal_existing_gui():
        return 0
    server = QLocalServer()
    QLocalServer.removeServer(_instance_name())
    server.listen(_instance_name())

    progress = QProgressDialog("Starting the Discord-Pro backend…", None, 0, 0)
    progress.setWindowTitle("Discord-Pro")
    progress.setMinimumDuration(400)
    holder: dict[str, object] = {}

    def started(info, error: str) -> None:
        progress.close()
        if info is None:
            again = QMessageBox.critical(None, "Discord-Pro", f"Could not start the backend.\n\n{error}",
                                         QMessageBox.StandardButton.Retry | QMessageBox.StandardButton.Close)
            if again == QMessageBox.StandardButton.Retry:
                progress.show()
                threading.Thread(target=starter.run, daemon=True).start()
            else:
                app.quit()
            return
        from discord_pro.gui.client import LiveChannel, RestClient
        from discord_pro.gui.main_window import MainWindow

        rest = RestClient(info)
        live = LiveChannel(info)
        window = MainWindow(rest, live)
        holder["window"] = window

        def on_new_connection() -> None:
            conn = server.nextPendingConnection()
            if conn is not None:
                conn.readyRead.connect(lambda: window.show_window())
                conn.disconnected.connect(conn.deleteLater)

        server.newConnection.connect(on_new_connection)
        live.start()
        window.show()
        app.aboutToQuit.connect(live.stop)

    starter = _Starter()
    starter.done.connect(started)
    threading.Thread(target=starter.run, daemon=True).start()
    progress.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
