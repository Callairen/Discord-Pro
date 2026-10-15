from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from fakes import FakeAdapter, FakeDiscordState  # noqa: E402

from helpers import BASE_URL  # noqa: E402


@pytest.fixture
def home(tmp_path, monkeypatch):
    d = tmp_path / "home"
    d.mkdir()
    monkeypatch.setenv("DISCORD_PRO_HOME", str(d))
    return d


@pytest.fixture
def discord():
    return FakeDiscordState()


@pytest.fixture
def fast_rpc():
    from discord_pro.backend.config import RpcSettings

    return RpcSettings(reconnect_max_attempts=5, reconnect_base_delay=0.05, reconnect_max_delay=0.2,
                       min_update_interval=0.0)


@pytest.fixture
def make_client(home, discord, fast_rpc):
    """Build a TestClient around a fresh app (state lives in ``home``)."""
    from fastapi.testclient import TestClient

    from discord_pro.backend.app import create_app

    clients = []

    def _make(*, peer: str = "127.0.0.1", base_url: str = BASE_URL, shutdown=None, raise_errors=True):
        app = create_app(adapter_factory=lambda: FakeAdapter(discord), rpc_settings=fast_rpc,
                         health_interval=0.05, request_shutdown=shutdown)
        client = TestClient(app, base_url=base_url, client=(peer, 50000), raise_server_exceptions=raise_errors)
        client.__enter__()
        clients.append(client)
        return client

    yield _make
    for c in clients:
        c.__exit__(None, None, None)


@pytest.fixture
def client(make_client):
    return make_client()


@pytest.fixture
def token(client):
    return client.app.state.backend.auth.api_token


@pytest.fixture
def auth(token):
    return {"Authorization": f"Bearer {token}"}


