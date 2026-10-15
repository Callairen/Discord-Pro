import shutil
import sys

import pytest

from fakes import FakeDiscordIPC, short_socket_dir
from harness import BackendProcess

from harness import CID  # noqa: F401

if sys.platform == "win32":  # the IPC simulator uses Unix sockets
    collect_ignore_glob = ["test_*.py"]


@pytest.fixture
def ipc(tmp_path):
    d = short_socket_dir(tmp_path)
    server = FakeDiscordIPC(d, valid_ids={CID})
    server.start()
    yield server
    server.stop()
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def backend(tmp_path, ipc):
    home = tmp_path / "home"
    home.mkdir()
    b = BackendProcess(home, ipc.dir)
    b.write_config()
    b.start()
    yield b
    b.stop()


def pytest_configure(config):
    sys.path.insert(0, __file__.rsplit("/", 1)[0])
