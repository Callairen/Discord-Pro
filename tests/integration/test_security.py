"""Authentication, CSRF, network-policy and rate-limit tests."""

import pytest

from discord_pro.backend.security import host_allowed, origin_allowed, peer_allowed, split_host


@pytest.mark.parametrize("host,lan,ok", [
    ("127.0.0.1:8765", False, True), ("localhost:8765", False, True), ("[::1]:8765", False, True),
    ("evil.example.com:8765", False, False), ("evil.example.com", True, False),
    ("192.168.1.20:8765", False, False), ("192.168.1.20:8765", True, True), ("8.8.8.8:8765", True, False),
    (None, False, False),
])
def test_host_policy(host, lan, ok):
    assert host_allowed(host, allow_lan=lan) is ok


def test_split_host():
    assert split_host("[::1]:80") == "::1" and split_host("a:1") == "a" and split_host("1.2.3.4") == "1.2.3.4"


def test_peer_policy():
    assert peer_allowed("127.0.0.1", allow_lan=False)
    assert not peer_allowed("192.168.1.5", allow_lan=False)
    assert peer_allowed("192.168.1.5", allow_lan=True)
    assert not peer_allowed("8.8.8.8", allow_lan=True)


def test_origin_policy():
    assert origin_allowed(None, "127.0.0.1:8765")
    assert origin_allowed("http://127.0.0.1:8765", "127.0.0.1:8765")
    assert not origin_allowed("http://evil.com", "127.0.0.1:8765")


def test_dns_rebinding_host_rejected(client, auth):
    r = client.get("/api/v1/state", headers={**auth, "Host": "attacker.example:8765"})
    assert r.status_code == 400 and r.json()["error"]["code"] == "invalid_host"


def test_lan_peer_rejected_when_lan_disabled(make_client):
    c = make_client(peer="192.168.1.50", base_url="http://192.168.1.10:8765")
    r = c.get("/api/v1/health")
    assert r.status_code == 403 and r.json()["error"]["code"] == "forbidden_network"


def _enable_lan_and_restart(make_client):
    c = make_client()
    a = {"Authorization": f"Bearer {c.app.state.backend.auth.api_token}"}
    c.put("/api/v1/config", headers=a, json={"server": {"allow_lan": True}})
    c.__exit__(None, None, None)
    return make_client(peer="192.168.1.50", base_url="http://192.168.1.10:8765")


def test_lan_requires_authentication(make_client):
    lan = _enable_lan_and_restart(make_client)
    assert lan.get("/api/v1/health").status_code == 200
    r = lan.get("/api/v1/state")
    assert r.status_code == 401  # AC-17: unauthorized LAN access rejected
    r = lan.post("/api/v1/presence/enable", headers={"X-Discord-Pro": "1"})
    assert r.status_code == 401
    with pytest.raises(Exception):
        with lan.websocket_connect("ws://192.168.1.10:8765/api/v1/ws") as ws:
            ws.receive_json()


def test_lan_login_with_access_code(make_client):
    lan = _enable_lan_and_restart(make_client)
    code = lan.app.state.backend.auth.access_code
    r = lan.post("/api/v1/auth/login", json={"access_code": code.lower().replace("-", "")})
    assert r.status_code == 200
    cookie = r.headers["set-cookie"]
    assert "HttpOnly" in cookie and "SameSite=strict" in cookie.replace("Strict", "strict")
    assert lan.get("/api/v1/state").status_code == 200
    # access code must not be readable by a browser session
    assert lan.get("/api/v1/auth/access-code").status_code == 403


def test_login_rate_limited(client):
    for _ in range(5):
        assert client.post("/api/v1/auth/login", json={"access_code": "WRONG-CODE-XX"}).status_code == 401
    r = client.post("/api/v1/auth/login", json={"access_code": client.app.state.backend.auth.access_code})
    assert r.status_code == 429


def test_session_csrf_protection(client):
    code = client.app.state.backend.auth.access_code
    client.post("/api/v1/auth/login", json={"access_code": code})
    # missing custom header -> rejected
    assert client.post("/api/v1/presence/disable").status_code == 403
    # cross-site origin -> rejected
    r = client.post("/api/v1/presence/disable", headers={"X-Discord-Pro": "1", "Origin": "http://evil.com"})
    assert r.status_code == 403
    r = client.post("/api/v1/presence/disable", headers={"X-Discord-Pro": "1", "Origin": "http://127.0.0.1:8765"})
    assert r.status_code == 200
    # cross-site login attempt rejected
    r = client.post("/api/v1/auth/login", json={"access_code": code}, headers={"Origin": "http://evil.com"})
    assert r.status_code == 403


def test_one_time_login_link(client, auth):
    assert client.post("/api/v1/auth/login-link").status_code == 401
    path = client.post("/api/v1/auth/login-link", headers=auth).json()["path"]
    r = client.get(path, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/" and "dp_session" in r.headers["set-cookie"]
    assert client.get("/api/v1/auth/session").json()["authenticated"] is True
    client.cookies.clear()
    r = client.get(path, follow_redirects=False)  # single use
    assert r.headers["location"] == "/?login=expired"


def test_rotate_access_code_revokes_sessions(client, auth):
    client.post("/api/v1/auth/login", json={"access_code": client.app.state.backend.auth.access_code})
    assert client.get("/api/v1/state").status_code == 200
    new = client.post("/api/v1/auth/access-code/rotate", headers=auth).json()["access_code"]
    assert client.get("/api/v1/state").status_code == 401
    assert client.post("/api/v1/auth/login", json={"access_code": new}).status_code == 200


def test_token_only_endpoints(client, auth):
    client.post("/api/v1/auth/login", json={"access_code": client.app.state.backend.auth.access_code})
    for path in ("/api/v1/auth/login-link", "/api/v1/auth/access-code/rotate", "/api/v1/shutdown"):
        assert client.post(path, headers={"X-Discord-Pro": "1"}).status_code == 403


def test_body_size_limit(client, auth):
    r = client.put("/api/v1/config", headers={**auth, "Content-Type": "application/json"},
                   content=b'{"x": "' + b"a" * 70000 + b'"}')
    assert r.status_code == 413


def test_command_rate_limit(client, auth):
    codes = [client.post("/api/v1/presence/disable", headers=auth).status_code for _ in range(35)]
    assert 429 in codes


def test_security_headers(client):
    r = client.get("/api/v1/health")
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
    assert r.headers["x-content-type-options"] == "nosniff"


def test_secrets_not_in_logs(client, auth, caplog, home):
    import logging

    caplog.set_level(logging.DEBUG)
    from discord_pro.backend.logging_setup import RedactingFilter

    logger = logging.getLogger("discord_pro.test")
    record = logger.makeRecord("x", logging.INFO, __file__, 1, "token=%s bearer %s",
                               (auth["Authorization"][7:], "Bearer abcdef123456"), None)
    RedactingFilter().filter(record)
    assert auth["Authorization"][7:] not in record.getMessage()
