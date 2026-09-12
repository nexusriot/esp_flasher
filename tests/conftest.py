"""Shared fixtures. Qt runs headless and QSettings is redirected to a tmpdir
so the suite can never touch the developer's real configuration."""

import os
import sys

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt6 import QtCore, QtWidgets  # noqa: E402

import esp_flasher as ef  # noqa: E402


@pytest.fixture(scope="session")
def qapp(tmp_path_factory):
    settings_dir = tmp_path_factory.mktemp("settings")
    QtCore.QSettings.setDefaultFormat(QtCore.QSettings.Format.IniFormat)
    QtCore.QSettings.setPath(QtCore.QSettings.Format.IniFormat,
                             QtCore.QSettings.Scope.UserScope, str(settings_dir))
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    app.setOrganizationName("EspFlasherTests")
    app.setApplicationName("EspFlasherTests")
    yield app


@pytest.fixture
def win(qapp):
    window = ef.EspFlasher()
    yield window
    window._port_timer.stop()
    window.deleteLater()


class FakeProcess:
    """Stands in for QProcess so stdout handling can be driven from a test."""

    def __init__(self, payload: bytes = b""):
        self._payload = payload
        self.terminated = False
        self.killed = False
        self.deleted = False

    def readAllStandardOutput(self):
        data, self._payload = self._payload, b""
        return QtCore.QByteArray(data)

    def state(self):
        return 2  # QProcess.ProcessState.Running

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True

    def errorString(self):
        return "fake error"

    def deleteLater(self):
        self.deleted = True


@pytest.fixture
def fake_process():
    return FakeProcess
